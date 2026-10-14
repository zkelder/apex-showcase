"""Use cases for the non-executing unified LIVE-intent preflight boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, TypeVar, cast

from apex.domain.cross_venue_allocation import (
    CrossVenueAllocationDecision,
    CrossVenueAllocationLine,
    PaperTrialProposal,
)
from apex.domain.live_preflight import (
    COINBASE_PREVIEW_HOST,
    COINBASE_PREVIEW_PATH,
    InternalPreflight,
    InternalPreflightInputs,
    InternalPreflightStatus,
    LiveIntentEligibilityDecision,
    LiveIntentEligibilityInputs,
    LiveIntentInvalidation,
    LiveIntentProposal,
    LiveOrderType,
    LivePreflightAssessment,
    LivePreflightError,
    ProviderPreviewApproval,
    ProviderPreviewAttempt,
    ProviderPreviewCapture,
    ProviderPreviewStatus,
    build_live_preflight_operating_tasks,
    coinbase_preview_body,
    evaluate_live_intent_eligibility,
    run_internal_preflight,
    validate_provider_preview_request,
)
from apex.domain.live_readiness import Provider, ReadinessRecommendation, redact_payload
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
)
from apex.domain.values import content_digest, parse_decimal

T = TypeVar("T")


class LivePreflightRepository(Protocol):
    def save_intent(self, value: LiveIntentProposal) -> LiveIntentProposal: ...
    def intent(self, intent_id: str) -> LiveIntentProposal | None: ...
    def intents(self) -> tuple[LiveIntentProposal, ...]: ...
    def save_invalidation(self, value: LiveIntentInvalidation) -> LiveIntentInvalidation: ...
    def invalidations(self, intent_id: str) -> tuple[LiveIntentInvalidation, ...]: ...
    def save_eligibility(
        self, value: LiveIntentEligibilityDecision
    ) -> LiveIntentEligibilityDecision: ...
    def eligibility(self, decision_id: str) -> LiveIntentEligibilityDecision | None: ...
    def save_preflight(self, value: InternalPreflight) -> InternalPreflight: ...
    def preflight(self, preflight_id: str) -> InternalPreflight | None: ...
    def preflights(self) -> tuple[InternalPreflight, ...]: ...
    def save_preview_approval(self, value: ProviderPreviewApproval) -> ProviderPreviewApproval: ...
    def preview_approval(self, approval_id: str) -> ProviderPreviewApproval | None: ...
    def preview_approvals(self) -> tuple[ProviderPreviewApproval, ...]: ...
    def save_preview_capture(self, value: ProviderPreviewCapture) -> ProviderPreviewCapture: ...
    def preview_capture(self, capture_id: str) -> ProviderPreviewCapture | None: ...
    def preview_captures(self) -> tuple[ProviderPreviewCapture, ...]: ...
    def save_preview_attempt(self, value: ProviderPreviewAttempt) -> ProviderPreviewAttempt: ...
    def preview_attempt(self, attempt_id: str) -> ProviderPreviewAttempt | None: ...
    def preview_attempts(self) -> tuple[ProviderPreviewAttempt, ...]: ...
    def save_assessment(self, value: LivePreflightAssessment) -> LivePreflightAssessment: ...
    def assessment(self, assessment_id: str) -> LivePreflightAssessment | None: ...
    def assessments(self) -> tuple[LivePreflightAssessment, ...]: ...


class CoinbasePreviewTransport(Protocol):
    def preview(
        self,
        *,
        method: str,
        host: str,
        path: str,
        body: Mapping[str, object],
        credential_reference_id: str,
        timeout_seconds: int,
    ) -> tuple[int, Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class ProviderPreviewRunResult:
    attempt: ProviderPreviewAttempt
    capture: ProviderPreviewCapture
    replayed: bool


class ConstructLiveIntent:
    """Construct an exact immutable contract from existing retained source objects."""

    def __init__(self, repository: LivePreflightRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        intent_id: str,
        projection: SharedOpportunityProjection,
        shared_eligibility: EligibilityDecision,
        allocation: CrossVenueAllocationDecision,
        allocation_line: CrossVenueAllocationLine,
        paper_trial_proposal: PaperTrialProposal,
        provider: Provider,
        account_scope_id: str,
        side: str,
        order_type: LiveOrderType,
        requested_quantity: Decimal | str | None,
        requested_notional: Decimal | str | None,
        limit_price: Decimal | str | None,
        maximum_risk: Decimal | str,
        strategy_or_method_version: str,
        capability_observed_at: datetime,
        created_at: datetime,
        expires_at: datetime,
        actor: str,
        confirmation: str,
    ) -> LiveIntentProposal:
        if confirmation != f"CONSTRUCT_LIVE_INTENT:{intent_id}" or not actor.strip():
            raise LivePreflightError("LIVE_INTENT_EXACT_OPERATOR_REVIEW_REQUIRED")
        expected_provider = {
            SharedVenue.CRYPTO: Provider.COINBASE,
            SharedVenue.KALSHI: Provider.KALSHI,
        }[projection.venue]
        if provider is not expected_provider:
            raise LivePreflightError("LIVE_INTENT_PROVIDER_LINEAGE_MISMATCH")
        if (
            shared_eligibility.projection_id != projection.projection_id
            or shared_eligibility.state is not EligibilityState.ELIGIBLE
            or allocation_line.decision_id != allocation.decision_id
            or allocation_line.source_projection_hash != projection.canonical_hash
            or allocation_line.projection_id != projection.projection_id
            or allocation_line.amount <= 0
            or allocation_line.line_id not in allocation.allocation_line_ids
            or paper_trial_proposal.proposal_id not in allocation.paper_trial_proposal_ids
            or paper_trial_proposal.decision_id != allocation.decision_id
            or paper_trial_proposal.allocation_line_id != allocation_line.line_id
            or paper_trial_proposal.source_projection_id != projection.projection_id
            or paper_trial_proposal.source_projection_hash != projection.canonical_hash
            or paper_trial_proposal.maximum_notional != allocation_line.amount
            or paper_trial_proposal.maximum_modeled_risk != allocation_line.modeled_downside
            or paper_trial_proposal.authorization
            or paper_trial_proposal.reservation
            or paper_trial_proposal.order
        ):
            raise LivePreflightError("LIVE_INTENT_SOURCE_LINEAGE_INVALID")
        risk = parse_decimal(maximum_risk)
        if risk > paper_trial_proposal.maximum_modeled_risk:
            raise LivePreflightError("LIVE_INTENT_RISK_EXCEEDS_TRIAL_PROPOSAL")
        if (
            requested_notional is not None
            and parse_decimal(requested_notional) > allocation_line.amount
        ):
            raise LivePreflightError("LIVE_INTENT_NOTIONAL_EXCEEDS_ALLOCATION")
        value = LiveIntentProposal.create(
            intent_id=intent_id,
            source_opportunity_id=projection.source_record_id,
            source_opportunity_hash=projection.source_canonical_hash,
            shared_projection_id=projection.projection_id,
            shared_projection_hash=projection.canonical_hash,
            allocation_decision_id=allocation.decision_id,
            allocation_decision_hash=allocation.canonical_hash,
            allocation_line_id=allocation_line.line_id,
            allocation_amount=allocation_line.amount,
            paper_trial_proposal_id=paper_trial_proposal.proposal_id,
            paper_trial_proposal_hash=paper_trial_proposal.canonical_hash,
            provider=provider,
            account_scope_id=account_scope_id,
            product_or_market_id=projection.product_or_market_id,
            side=side,
            order_type=order_type,
            requested_quantity=requested_quantity,
            requested_notional=requested_notional,
            limit_price=limit_price,
            maximum_risk=risk,
            strategy_or_method_version=strategy_or_method_version,
            opportunity_observed_at=projection.generated_at,
            allocation_decided_at=allocation.decided_at,
            capability_observed_at=capability_observed_at,
            created_at=created_at,
            expires_at=expires_at,
        )
        return self._repository.save_intent(value)


class EvaluateLiveIntentEligibility:
    def __init__(self, repository: LivePreflightRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        decision_id: str,
        intent_id: str,
        inputs: LiveIntentEligibilityInputs,
    ) -> LiveIntentEligibilityDecision:
        intent = self._repository.intent(intent_id)
        if intent is None:
            raise LivePreflightError("LIVE_INTENT_NOT_FOUND")
        if self._repository.invalidations(intent_id):
            raise LivePreflightError("LIVE_INTENT_INVALIDATED")
        return self._repository.save_eligibility(
            evaluate_live_intent_eligibility(
                decision_id=decision_id,
                intent=intent,
                inputs=inputs,
            )
        )


class RunInternalPreflight:
    def __init__(self, repository: LivePreflightRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        preflight_id: str,
        intent_id: str,
        eligibility_decision_id: str,
        capability_snapshot_id: str,
        capability_snapshot_hash: str,
        readiness_assessment_id: str,
        readiness_assessment_hash: str,
        readiness_recommendation: ReadinessRecommendation,
        risk_decision_id: str,
        risk_decision_hash: str,
        inputs: InternalPreflightInputs,
    ) -> InternalPreflight:
        intent = self._repository.intent(intent_id)
        eligibility = self._repository.eligibility(eligibility_decision_id)
        if intent is None or eligibility is None or eligibility.intent_id != intent.intent_id:
            raise LivePreflightError("LIVE_PREFLIGHT_INTENT_ELIGIBILITY_LINEAGE_INVALID")
        if self._repository.invalidations(intent_id):
            raise LivePreflightError("LIVE_INTENT_INVALIDATED")
        return self._repository.save_preflight(
            run_internal_preflight(
                preflight_id=preflight_id,
                intent=intent,
                eligibility=eligibility,
                capability_snapshot_id=capability_snapshot_id,
                capability_snapshot_hash=capability_snapshot_hash,
                readiness_assessment_id=readiness_assessment_id,
                readiness_assessment_hash=readiness_assessment_hash,
                readiness_recommendation=readiness_recommendation,
                risk_decision_id=risk_decision_id,
                risk_decision_hash=risk_decision_hash,
                inputs=inputs,
            )
        )


class ApproveCoinbaseProviderPreview:
    def __init__(self, repository: LivePreflightRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        approval_id: str,
        credential_reference_id: str,
        intent_id: str,
        maximum_request_count: int,
        timeout_seconds: int,
        approved_at: datetime,
        expires_at: datetime,
        actor: str,
        reason: str,
        idempotency_key: str,
        confirmation: str,
    ) -> ProviderPreviewApproval:
        intent = self._repository.intent(intent_id)
        if intent is None:
            raise LivePreflightError("LIVE_INTENT_NOT_FOUND")
        if self._repository.invalidations(intent_id):
            raise LivePreflightError("LIVE_INTENT_INVALIDATED")
        expected = f"APPROVE_COINBASE_PROVIDER_PREVIEW:{approval_id}:{intent_id}"
        if confirmation != expected:
            raise LivePreflightError("PROVIDER_PREVIEW_EXACT_CONFIRMATION_REQUIRED")
        return self._repository.save_preview_approval(
            ProviderPreviewApproval.create_coinbase(
                approval_id=approval_id,
                credential_reference_id=credential_reference_id,
                intent=intent,
                maximum_request_count=maximum_request_count,
                timeout_seconds=timeout_seconds,
                approved_at=approved_at,
                expires_at=expires_at,
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        )


def _text_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(str(item) for item in value)


class RunApprovedCoinbaseProviderPreview:
    def __init__(
        self,
        repository: LivePreflightRepository,
        transport: CoinbasePreviewTransport,
    ) -> None:
        self._repository = repository
        self._transport = transport

    def execute(
        self,
        *,
        capture_id: str,
        approval_id: str,
        preflight_id: str,
        requested_at: datetime,
    ) -> ProviderPreviewRunResult:
        retained = self._repository.preview_capture(capture_id)
        if retained is not None:
            attempt = self._repository.preview_attempt(f"{capture_id}-attempt")
            if attempt is None:
                raise LivePreflightError("PROVIDER_PREVIEW_ATTEMPT_LINEAGE_MISSING")
            return ProviderPreviewRunResult(attempt, retained, True)
        approval = self._repository.preview_approval(approval_id)
        preflight = self._repository.preflight(preflight_id)
        if approval is None or preflight is None:
            raise LivePreflightError("PROVIDER_PREVIEW_APPROVAL_OR_PREFLIGHT_NOT_FOUND")
        intent = self._repository.intent(preflight.intent_id)
        if intent is None:
            raise LivePreflightError("LIVE_INTENT_NOT_FOUND")
        if self._repository.invalidations(intent.intent_id):
            raise LivePreflightError("LIVE_INTENT_INVALIDATED")
        if preflight.status is not InternalPreflightStatus.READY_FOR_PROVIDER_PREVIEW:
            raise LivePreflightError("PROVIDER_PREVIEW_INTERNAL_PREFLIGHT_NOT_READY")
        body = coinbase_preview_body(intent)
        prior_count = sum(
            item.request_count
            for item in self._repository.preview_attempts()
            if item.approval_id == approval.approval_id
        )
        attempt_id = f"{capture_id}-attempt"
        try:
            validate_provider_preview_request(
                approval=approval,
                intent=intent,
                method="POST",
                host=COINBASE_PREVIEW_HOST,
                path=COINBASE_PREVIEW_PATH,
                body=body,
                requested_at=requested_at,
                prior_request_count=prior_count,
            )
        except LivePreflightError as error:
            self._repository.save_preview_attempt(
                ProviderPreviewAttempt.create(
                    attempt_id=attempt_id,
                    approval_id=approval.approval_id,
                    intent=intent,
                    status=ProviderPreviewStatus.BLOCKED,
                    capture_id=None,
                    blocked_reason=str(error),
                    attempted_at=requested_at,
                    request_count=0,
                )
            )
            raise
        try:
            status_code, raw = self._transport.preview(
                method="POST",
                host=COINBASE_PREVIEW_HOST,
                path=COINBASE_PREVIEW_PATH,
                body=body,
                credential_reference_id=approval.credential_reference_id,
                timeout_seconds=approval.timeout_seconds,
            )
        except Exception as error:
            self._repository.save_preview_attempt(
                ProviderPreviewAttempt.create(
                    attempt_id=attempt_id,
                    approval_id=approval.approval_id,
                    intent=intent,
                    status=ProviderPreviewStatus.PROVIDER_REJECTED,
                    capture_id=None,
                    blocked_reason=type(error).__name__,
                    attempted_at=requested_at,
                    request_count=1,
                )
            )
            raise
        sanitized = redact_payload(raw)
        if not isinstance(sanitized, Mapping):
            self._repository.save_preview_attempt(
                ProviderPreviewAttempt.create(
                    attempt_id=attempt_id,
                    approval_id=approval.approval_id,
                    intent=intent,
                    status=ProviderPreviewStatus.PROVIDER_REJECTED,
                    capture_id=None,
                    blocked_reason="PROVIDER_PREVIEW_RESPONSE_INVALID",
                    attempted_at=requested_at,
                    request_count=1,
                )
            )
            raise LivePreflightError("PROVIDER_PREVIEW_RESPONSE_INVALID")
        response_hash = content_digest((status_code, sanitized))
        validations = list(_text_sequence(sanitized.get("errs")))
        if status_code != 200:
            validations.append(f"HTTP_STATUS_{status_code}")
        warnings = _text_sequence(sanitized.get("warning"))
        fee_value = sanitized.get("commission_total")
        estimated_fees = None
        if fee_value not in (None, ""):
            try:
                estimated_fees = parse_decimal(cast(str | int | Decimal, fee_value))
            except (TypeError, ValueError, ArithmeticError):
                validations.append("PROVIDER_PREVIEW_FEE_INVALID")
        expected_body = coinbase_preview_body(intent)
        mismatch = any(
            key in sanitized and sanitized[key] != expected
            for key, expected in (
                ("product_id", expected_body["product_id"]),
                ("side", expected_body["side"]),
            )
        )
        expected_size = intent.requested_quantity or intent.requested_notional
        if intent.requested_quantity is not None and "base_size" in sanitized:
            try:
                mismatch = (
                    mismatch or parse_decimal(cast(str, sanitized["base_size"])) != expected_size
                )
            except (TypeError, ValueError, ArithmeticError):
                validations.append("PROVIDER_PREVIEW_BASE_SIZE_INVALID")
                mismatch = True
        if intent.requested_notional is not None and "quote_size" in sanitized:
            try:
                mismatch = (
                    mismatch or parse_decimal(cast(str, sanitized["quote_size"])) != expected_size
                )
            except (TypeError, ValueError, ArithmeticError):
                validations.append("PROVIDER_PREVIEW_QUOTE_SIZE_INVALID")
                mismatch = True
        constraints = tuple(
            (key.upper(), str(sanitized[key]))
            for key in ("best_bid", "best_ask", "order_total", "slippage")
            if key in sanitized
        )
        capture = ProviderPreviewCapture.create_coinbase(
            capture_id=capture_id,
            approval=approval,
            intent=intent,
            request_identity=content_digest((approval.canonical_hash, intent.canonical_hash, body)),
            sanitized_response_hash=response_hash,
            provider_validation_results=validations,
            estimated_fees=estimated_fees,
            product_constraints=constraints,
            warnings=warnings,
            captured_at=requested_at,
            mismatch=mismatch,
        )
        retained_capture = self._repository.save_preview_capture(capture)
        attempt = self._repository.save_preview_attempt(
            ProviderPreviewAttempt.create(
                attempt_id=attempt_id,
                approval_id=approval.approval_id,
                intent=intent,
                status=capture.status,
                capture_id=capture.capture_id,
                blocked_reason=None,
                attempted_at=requested_at,
                request_count=1,
            )
        )
        return ProviderPreviewRunResult(attempt, retained_capture, False)


class RetainKalshiPreviewUnavailable:
    def __init__(self, repository: LivePreflightRepository) -> None:
        self._repository = repository

    def execute(
        self, *, capture_id: str, intent_id: str, captured_at: datetime
    ) -> ProviderPreviewCapture:
        intent = self._repository.intent(intent_id)
        if intent is None:
            raise LivePreflightError("LIVE_INTENT_NOT_FOUND")
        if self._repository.invalidations(intent_id):
            raise LivePreflightError("LIVE_INTENT_INVALIDATED")
        capture = self._repository.save_preview_capture(
            ProviderPreviewCapture.unavailable(
                capture_id=capture_id,
                intent=intent,
                captured_at=captured_at,
            )
        )
        self._repository.save_preview_attempt(
            ProviderPreviewAttempt.create(
                attempt_id=f"{capture_id}-attempt",
                approval_id=None,
                intent=intent,
                status=ProviderPreviewStatus.UNAVAILABLE,
                capture_id=capture.capture_id,
                blocked_reason="KALSHI_NON_MUTATING_ORDER_PREVIEW_NOT_DOCUMENTED",
                attempted_at=captured_at,
                request_count=0,
            )
        )
        return capture


class MaterializeLivePreflightAssessment:
    def __init__(self, repository: LivePreflightRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        assessment_id: str,
        preflight_id: str,
        preview_capture_id: str | None,
        assessed_at: datetime,
        readiness_blockers_before: tuple[str, ...] = (),
    ) -> LivePreflightAssessment:
        preflight = self._repository.preflight(preflight_id)
        preview = (
            None
            if preview_capture_id is None
            else self._repository.preview_capture(preview_capture_id)
        )
        if preflight is None or (preview_capture_id is not None and preview is None):
            raise LivePreflightError("LIVE_PREFLIGHT_ASSESSMENT_SOURCE_NOT_FOUND")
        if preview is not None and preview.intent_id != preflight.intent_id:
            raise LivePreflightError("LIVE_PREFLIGHT_PREVIEW_LINEAGE_MISMATCH")
        return self._repository.save_assessment(
            LivePreflightAssessment.create(
                assessment_id=assessment_id,
                preflight=preflight,
                preview=preview,
                readiness_blockers_before=readiness_blockers_before,
                assessed_at=assessed_at,
            )
        )


class InvalidateLiveIntent:
    def __init__(self, repository: LivePreflightRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        intent_id: str,
        invalidated_at: datetime,
        reason: str,
        confirmation: str,
    ) -> LiveIntentInvalidation:
        intent = self._repository.intent(intent_id)
        if intent is None:
            raise LivePreflightError("LIVE_INTENT_NOT_FOUND")
        if confirmation != f"INVALIDATE_LIVE_INTENT:{intent_id}":
            raise LivePreflightError("LIVE_INTENT_INVALIDATION_CONFIRMATION_REQUIRED")
        return self._repository.save_invalidation(
            intent.invalidate(invalidated_at=invalidated_at, reason=reason)
        )


class InMemoryLivePreflightRepository:
    def __init__(self) -> None:
        self._intents: dict[str, LiveIntentProposal] = {}
        self._invalidations: dict[str, LiveIntentInvalidation] = {}
        self._eligibility: dict[str, LiveIntentEligibilityDecision] = {}
        self._preflights: dict[str, InternalPreflight] = {}
        self._approvals: dict[str, ProviderPreviewApproval] = {}
        self._captures: dict[str, ProviderPreviewCapture] = {}
        self._attempts: dict[str, ProviderPreviewAttempt] = {}
        self._assessments: dict[str, LivePreflightAssessment] = {}

    @staticmethod
    def _save(values: dict[str, T], key: str, value: T) -> T:
        retained = values.get(key)
        if (
            retained is not None
            and cast(Any, retained).canonical_hash != cast(Any, value).canonical_hash
        ):
            raise LivePreflightError("LIVE_PREFLIGHT_ID_CONFLICT")
        values[key] = value
        return value

    def save_intent(self, value: LiveIntentProposal) -> LiveIntentProposal:
        return self._save(self._intents, value.intent_id, value)

    def intent(self, intent_id: str) -> LiveIntentProposal | None:
        return self._intents.get(intent_id)

    def intents(self) -> tuple[LiveIntentProposal, ...]:
        return tuple(self._intents[key] for key in sorted(self._intents))

    def save_invalidation(self, value: LiveIntentInvalidation) -> LiveIntentInvalidation:
        return self._save(self._invalidations, value.invalidation_id, value)

    def invalidations(self, intent_id: str) -> tuple[LiveIntentInvalidation, ...]:
        return tuple(item for item in self._invalidations.values() if item.intent_id == intent_id)

    def save_eligibility(
        self, value: LiveIntentEligibilityDecision
    ) -> LiveIntentEligibilityDecision:
        return self._save(self._eligibility, value.decision_id, value)

    def eligibility(self, decision_id: str) -> LiveIntentEligibilityDecision | None:
        return self._eligibility.get(decision_id)

    def save_preflight(self, value: InternalPreflight) -> InternalPreflight:
        return self._save(self._preflights, value.preflight_id, value)

    def preflight(self, preflight_id: str) -> InternalPreflight | None:
        return self._preflights.get(preflight_id)

    def preflights(self) -> tuple[InternalPreflight, ...]:
        return tuple(self._preflights[key] for key in sorted(self._preflights))

    def save_preview_approval(self, value: ProviderPreviewApproval) -> ProviderPreviewApproval:
        return self._save(self._approvals, value.approval_id, value)

    def preview_approval(self, approval_id: str) -> ProviderPreviewApproval | None:
        return self._approvals.get(approval_id)

    def preview_approvals(self) -> tuple[ProviderPreviewApproval, ...]:
        return tuple(self._approvals[key] for key in sorted(self._approvals))

    def save_preview_capture(self, value: ProviderPreviewCapture) -> ProviderPreviewCapture:
        return self._save(self._captures, value.capture_id, value)

    def preview_capture(self, capture_id: str) -> ProviderPreviewCapture | None:
        return self._captures.get(capture_id)

    def preview_captures(self) -> tuple[ProviderPreviewCapture, ...]:
        return tuple(self._captures[key] for key in sorted(self._captures))

    def save_preview_attempt(self, value: ProviderPreviewAttempt) -> ProviderPreviewAttempt:
        return self._save(self._attempts, value.attempt_id, value)

    def preview_attempt(self, attempt_id: str) -> ProviderPreviewAttempt | None:
        return self._attempts.get(attempt_id)

    def preview_attempts(self) -> tuple[ProviderPreviewAttempt, ...]:
        return tuple(self._attempts[key] for key in sorted(self._attempts))

    def save_assessment(self, value: LivePreflightAssessment) -> LivePreflightAssessment:
        return self._save(self._assessments, value.assessment_id, value)

    def assessment(self, assessment_id: str) -> LivePreflightAssessment | None:
        return self._assessments.get(assessment_id)

    def assessments(self) -> tuple[LivePreflightAssessment, ...]:
        return tuple(self._assessments[key] for key in sorted(self._assessments))


def live_preflight_workspace(
    repository: LivePreflightRepository, *, as_of: datetime
) -> dict[str, object]:
    intents = repository.intents()
    active_intents = tuple(item for item in intents if not repository.invalidations(item.intent_id))
    preflights = repository.preflights()
    approvals = repository.preview_approvals()
    captures = repository.preview_captures()
    attempts = repository.preview_attempts()
    assessments = repository.assessments()
    return {
        "intents": intents,
        "preflights": preflights,
        "provider_preview_approvals": approvals,
        "provider_preview_captures": captures,
        "provider_preview_attempts": attempts,
        "assessments": assessments,
        "operating_cycle_tasks": build_live_preflight_operating_tasks(
            intents=active_intents,
            preflights=preflights,
            approvals=approvals,
            assessments=assessments,
            as_of=as_of,
        ),
        "safe_actions": (
            "CONSTRUCT_FROM_NONZERO_AUTHORITY_FREE_TRIAL_PROPOSAL",
            "RUN_INTERNAL_PREFLIGHT",
            "REQUEST_OR_INSPECT_EXACT_PROVIDER_PREVIEW_APPROVAL",
            "RUN_APPROVED_COINBASE_PREVIEW",
            "INSPECT_EVIDENCE",
            "EXPIRE_OR_INVALIDATE_INTENT",
        ),
        "prohibited_actions": (
            "SUBMIT",
            "CANCEL",
            "REPLACE",
            "TRANSFER",
            "WITHDRAW",
            "LIVE_PILOT_ARMED",
        ),
        "provider_preview_support": {
            "COINBASE": "SUPPORTED_SEPARATE_EXACT_APPROVAL_REQUIRED",
            "KALSHI": "PROVIDER_PREVIEW_UNAVAILABLE",
        },
        "provider_request_count": sum(item.request_count for item in attempts),
        "provider_write_count": 0,
        "authority": "NONE",
        "live_authority": "NONE",
    }
