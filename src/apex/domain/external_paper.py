"""Pure external-broker PAPER contracts; never grants LIVE authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import content_digest, deterministic_id, parse_decimal

ALPACA_PAPER_BROKER_V1 = "ALPACA_PAPER_BROKER_V1"
ALPACA_PAPER_DAILY_LONG_ONLY_V1 = "ALPACA_PAPER_DAILY_LONG_ONLY_V1"
APEX_PAPER_BROKER_INSTRUCTION_V1 = "APEX_PAPER_BROKER_INSTRUCTION_V1"
APEX_EXTERNAL_PAPER_RECONCILIATION_V1 = "APEX_EXTERNAL_PAPER_RECONCILIATION_V1"
APEX_VALIDATED_EXTERNAL_PAPER_FILL_V1 = "APEX_VALIDATED_EXTERNAL_PAPER_FILL_V1"
APEX_EXTERNAL_PAPER_INTERNAL_EXECUTION_BINDING_V1 = (
    "APEX_EXTERNAL_PAPER_INTERNAL_EXECUTION_BINDING_V1"
)
APEX_EXTERNAL_PAPER_CAMPAIGN_OBSERVATION_V1 = "APEX_EXTERNAL_PAPER_CAMPAIGN_OBSERVATION_V1"


class ExternalPaperBrokerError(RuntimeError):
    """A bounded Paper broker transport rejected or could not complete an operation."""


class ExternalPaperSubmissionAmbiguous(ExternalPaperBrokerError):
    """Network outcome is unknown; the instruction must be reconciled, never retried."""


class ExternalPaperCapabilityState(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"
    AUTHENTICATION_REJECTED = "AUTHENTICATION_REJECTED"
    PERMISSION_LIMITED = "PERMISSION_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    BLOCKED_CONFIGURATION = "BLOCKED_CONFIGURATION"


class ExternalPaperInstructionState(StrEnum):
    PROPOSED = "PROPOSED"
    AUTHORIZED = "AUTHORIZED"
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ExternalPaperSubmissionState(StrEnum):
    RESERVED = "RESERVED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    AMBIGUOUS = "AMBIGUOUS"


class ExternalPaperKillState(StrEnum):
    ACTIVE = "ACTIVE"
    KILLED = "KILLED"


class ValidatedExternalPaperFillState(StrEnum):
    OBSERVED = "OBSERVED"
    VALIDATED = "VALIDATED"
    APPLIED = "APPLIED"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"
    DUPLICATE = "DUPLICATE"
    BLOCKED_AMBIGUOUS = "BLOCKED_AMBIGUOUS"
    BLOCKED_RECONCILIATION = "BLOCKED_RECONCILIATION"
    REVERSED_BY_CORRECTION = "REVERSED_BY_CORRECTION"


class ExternalPaperInternalExecutionBindingState(StrEnum):
    REQUESTED = "REQUESTED"
    BOUND = "BOUND"
    ORDER_PENDING_EXTERNAL_FILL = "ORDER_PENDING_EXTERNAL_FILL"
    PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
    FULLY_APPLIED = "FULLY_APPLIED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    BLOCKED_AMBIGUOUS = "BLOCKED_AMBIGUOUS"
    BLOCKED_RECONCILIATION = "BLOCKED_RECONCILIATION"
    CLOSED = "CLOSED"


class ExternalPaperReconciliationClassification(StrEnum):
    RECONCILED = "RECONCILED"
    EXPLAINED_DIFFERENCE = "EXPLAINED_DIFFERENCE"
    MATERIAL_DISCREPANCY = "MATERIAL_DISCREPANCY"
    BROKER_STATE_INCOMPLETE = "BROKER_STATE_INCOMPLETE"
    APEX_STATE_INCOMPLETE = "APEX_STATE_INCOMPLETE"
    SUBMISSION_AMBIGUOUS = "SUBMISSION_AMBIGUOUS"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PaperBrokerCapabilitySnapshot:
    snapshot_id: str
    state: ExternalPaperCapabilityState
    credentials_available: bool
    authentication_state: str
    account_read: bool
    order_submit: bool
    order_read: bool
    cancel: bool
    position_read: bool
    activity_read: bool
    checked_at: datetime
    reason_codes: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        state: ExternalPaperCapabilityState,
        credentials_available: bool,
        authentication_state: str,
        account_read: bool,
        order_submit: bool,
        order_read: bool,
        cancel: bool,
        position_read: bool,
        activity_read: bool,
        checked_at: datetime,
        reason_codes: tuple[str, ...] = (),
    ) -> PaperBrokerCapabilitySnapshot:
        checked = _aware(checked_at, "capability timestamp")
        payload = (
            ALPACA_PAPER_BROKER_V1,
            "PAPER",
            state,
            credentials_available,
            authentication_state,
            account_read,
            order_submit,
            order_read,
            cancel,
            position_read,
            activity_read,
            checked,
            tuple(sorted(reason_codes)),
        )
        return cls(
            deterministic_id("external_paper_capability", payload),
            state,
            credentials_available,
            authentication_state,
            account_read,
            order_submit,
            order_read,
            cancel,
            position_read,
            activity_read,
            checked,
            tuple(sorted(reason_codes)),
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperAuthorization:
    authorization_id: str
    campaign_id: str
    paper_trial_authorization_id: str
    portfolio_run_id: str
    portfolio_instruction_id: str
    allowed_symbols: tuple[str, ...]
    maximum_capital: Decimal
    maximum_position_quantity: Decimal
    maximum_order_count: int
    starts_at: datetime
    expires_at: datetime
    operator_action_id: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        paper_trial_authorization_id: str,
        portfolio_run_id: str,
        portfolio_instruction_id: str,
        allowed_symbols: tuple[str, ...],
        maximum_capital: str,
        maximum_position_quantity: str,
        maximum_order_count: int,
        starts_at: datetime,
        expires_at: datetime,
        operator_action_id: str,
    ) -> ExternalPaperAuthorization:
        start, expiry = (
            _aware(starts_at, "authorization start"),
            _aware(expires_at, "authorization expiry"),
        )
        capital = parse_decimal(maximum_capital, allow_zero=False)
        quantity = parse_decimal(maximum_position_quantity, allow_zero=False)
        symbols = tuple(sorted(set(allowed_symbols)))
        if not campaign_id or not paper_trial_authorization_id or not portfolio_instruction_id:
            raise ValueError("external PAPER authorization identities are required")
        if not symbols or any(not symbol.isupper() for symbol in symbols):
            raise ValueError("external PAPER authorization symbols are invalid")
        if start >= expiry or maximum_order_count < 1 or not operator_action_id:
            raise ValueError("external PAPER authorization bounds are invalid")
        payload = (
            ALPACA_PAPER_DAILY_LONG_ONLY_V1,
            campaign_id,
            paper_trial_authorization_id,
            portfolio_run_id,
            portfolio_instruction_id,
            symbols,
            capital,
            quantity,
            maximum_order_count,
            start,
            expiry,
            operator_action_id,
            "PAPER",
            "LONG_ONLY",
            "DAY",
        )
        return cls(
            deterministic_id("external_paper_authorization", payload),
            campaign_id,
            paper_trial_authorization_id,
            portfolio_run_id,
            portfolio_instruction_id,
            symbols,
            capital,
            quantity,
            maximum_order_count,
            start,
            expiry,
            operator_action_id,
            content_digest(payload),
        )

    def active_at(self, at: datetime) -> bool:
        moment = _aware(at, "authorization validation")
        return self.starts_at <= moment < self.expires_at


@dataclass(frozen=True, slots=True)
class ExternalPaperSubmissionAttempt:
    """An immutable pre-I/O reservation; no retry may replace an ambiguous attempt."""

    attempt_id: str
    instruction_id: str
    ordinal: int
    reserved_at: datetime
    endpoint_identity: str
    request_hash: str
    state: ExternalPaperSubmissionState
    broker_order_id: str | None
    broker_request_id: str | None
    ambiguity_reason: str | None
    canonical_hash: str

    @classmethod
    def reserve(
        cls,
        *,
        instruction_id: str,
        ordinal: int,
        reserved_at: datetime,
        request_hash: str,
    ) -> ExternalPaperSubmissionAttempt:
        if not instruction_id or ordinal < 1 or not request_hash:
            raise ValueError("external PAPER submission reservation is invalid")
        reserved = _aware(reserved_at, "submission reservation timestamp")
        payload = (
            APEX_PAPER_BROKER_INSTRUCTION_V1,
            instruction_id,
            ordinal,
            reserved,
            "PAPER:POST:/v2/orders",
            request_hash,
            ExternalPaperSubmissionState.RESERVED,
        )
        return cls(
            deterministic_id("external_paper_submission_attempt", payload),
            instruction_id,
            ordinal,
            reserved,
            "PAPER:POST:/v2/orders",
            request_hash,
            ExternalPaperSubmissionState.RESERVED,
            None,
            None,
            None,
            content_digest(payload),
        )

    def conclude(
        self,
        *,
        state: ExternalPaperSubmissionState,
        broker_order_id: str | None = None,
        broker_request_id: str | None = None,
        ambiguity_reason: str | None = None,
    ) -> ExternalPaperSubmissionAttempt:
        if (
            self.state
            not in {ExternalPaperSubmissionState.RESERVED, ExternalPaperSubmissionState.AMBIGUOUS}
            or state is ExternalPaperSubmissionState.RESERVED
            or (
                self.state is ExternalPaperSubmissionState.AMBIGUOUS
                and state is not ExternalPaperSubmissionState.ACKNOWLEDGED
            )
        ):
            raise ValueError("external PAPER submission attempt transition is invalid")
        if state is ExternalPaperSubmissionState.AMBIGUOUS and not ambiguity_reason:
            raise ValueError("ambiguous submission requires a reason")
        return type(self)(
            self.attempt_id,
            self.instruction_id,
            self.ordinal,
            self.reserved_at,
            self.endpoint_identity,
            self.request_hash,
            state,
            broker_order_id,
            broker_request_id,
            ambiguity_reason,
            self.canonical_hash,
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperInstruction:
    instruction_id: str
    authorization_id: str
    campaign_id: str
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    time_in_force: str
    maximum_notional: Decimal
    expires_at: datetime
    idempotency_key: str
    state: ExternalPaperInstructionState
    broker_order_id: str | None
    reason_code: str | None
    canonical_hash: str

    @classmethod
    def authorized(
        cls,
        *,
        authorization: ExternalPaperAuthorization,
        symbol: str,
        quantity: str,
        maximum_notional: str,
        expires_at: datetime,
        idempotency_key: str,
        side: str = "BUY",
    ) -> ExternalPaperInstruction:
        expiry = _aware(expires_at, "instruction expiry")
        parsed_quantity = parse_decimal(quantity, allow_zero=False)
        notional = parse_decimal(maximum_notional, allow_zero=False)
        normalized_side = side.upper()
        if (
            symbol not in authorization.allowed_symbols
            or parsed_quantity > authorization.maximum_position_quantity
            or notional > authorization.maximum_capital
            or expiry > authorization.expires_at
            or not idempotency_key
            or normalized_side not in {"BUY", "SELL"}
        ):
            raise ValueError("external PAPER instruction exceeds authorization")
        payload = (
            APEX_PAPER_BROKER_INSTRUCTION_V1,
            authorization.authorization_id,
            authorization.campaign_id,
            symbol,
            normalized_side,
            parsed_quantity,
            "MARKET",
            "DAY",
            notional,
            expiry,
            idempotency_key,
        )
        return cls(
            deterministic_id("external_paper_instruction", payload),
            authorization.authorization_id,
            authorization.campaign_id,
            symbol,
            normalized_side,
            parsed_quantity,
            "MARKET",
            "DAY",
            notional,
            expiry,
            idempotency_key,
            ExternalPaperInstructionState.AUTHORIZED,
            None,
            None,
            content_digest(payload),
        )

    def transition(
        self,
        *,
        state: ExternalPaperInstructionState,
        broker_order_id: str | None = None,
        reason_code: str | None = None,
    ) -> ExternalPaperInstruction:
        allowed = {
            ExternalPaperInstructionState.AUTHORIZED: {
                ExternalPaperInstructionState.SUBMISSION_PENDING,
                ExternalPaperInstructionState.BLOCKED,
            },
            ExternalPaperInstructionState.SUBMISSION_PENDING: {
                ExternalPaperInstructionState.SUBMITTED,
                ExternalPaperInstructionState.REJECTED,
                ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
            },
            ExternalPaperInstructionState.SUBMITTED: {
                ExternalPaperInstructionState.PARTIALLY_FILLED,
                ExternalPaperInstructionState.FILLED,
                ExternalPaperInstructionState.CANCELED,
                ExternalPaperInstructionState.REJECTED,
                ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
            },
            ExternalPaperInstructionState.PARTIALLY_FILLED: {
                ExternalPaperInstructionState.FILLED,
                ExternalPaperInstructionState.CANCELED,
                ExternalPaperInstructionState.RECONCILIATION_REQUIRED,
            },
            ExternalPaperInstructionState.RECONCILIATION_REQUIRED: {
                ExternalPaperInstructionState.SUBMITTED,
                ExternalPaperInstructionState.PARTIALLY_FILLED,
                ExternalPaperInstructionState.FILLED,
                ExternalPaperInstructionState.CANCELED,
                ExternalPaperInstructionState.REJECTED,
            },
        }
        if state not in allowed.get(self.state, set()):
            raise ValueError("external PAPER instruction transition is invalid")
        return ExternalPaperInstruction(
            self.instruction_id,
            self.authorization_id,
            self.campaign_id,
            self.symbol,
            self.side,
            self.quantity,
            self.order_type,
            self.time_in_force,
            self.maximum_notional,
            self.expires_at,
            self.idempotency_key,
            state,
            broker_order_id or self.broker_order_id,
            reason_code,
            self.canonical_hash,
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperReconciliation:
    reconciliation_id: str
    instruction_id: str
    classification: ExternalPaperReconciliationClassification
    differing_field: str | None
    apex_value: str | None
    broker_value: str | None
    required_action: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ExternalPaperBrokerEvidence:
    evidence_id: str
    instruction_id: str | None
    endpoint_identity: str
    observation_type: str
    observed_at: datetime
    broker_reported_at: datetime | None
    request_id: str | None
    sanitized_payload: dict[str, str | None]
    payload_hash: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        instruction_id: str | None,
        endpoint_identity: str,
        observation_type: str,
        observed_at: datetime,
        broker_reported_at: datetime | None,
        request_id: str | None,
        sanitized_payload: dict[str, str | None],
    ) -> ExternalPaperBrokerEvidence:
        observed = _aware(observed_at, "broker evidence timestamp")
        reported = (
            _aware(broker_reported_at, "broker reported timestamp")
            if broker_reported_at is not None
            else None
        )
        if not endpoint_identity.startswith("PAPER:"):
            raise ValueError("broker evidence endpoint is not Paper-only")
        payload = (
            ALPACA_PAPER_BROKER_V1,
            instruction_id,
            endpoint_identity,
            observation_type,
            observed,
            reported,
            request_id,
            sanitized_payload,
        )
        return cls(
            deterministic_id("external_paper_broker_evidence", payload),
            instruction_id,
            endpoint_identity,
            observation_type,
            observed,
            reported,
            request_id,
            dict(sorted(sanitized_payload.items())),
            content_digest(sanitized_payload),
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperCampaignObservation:
    """Immutable bounded broker observation; never changes Apex financial state."""

    observation_id: str
    campaign_id: str
    evidence_id: str
    observation_type: str
    known_instruction_ids: tuple[str, ...]
    unknown_broker_activity: bool
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        evidence: ExternalPaperBrokerEvidence,
        known_instruction_ids: tuple[str, ...] = (),
        unknown_broker_activity: bool = False,
    ) -> ExternalPaperCampaignObservation:
        if not campaign_id:
            raise ValueError("campaign observation requires a campaign")
        known = tuple(sorted(set(known_instruction_ids)))
        payload = (
            APEX_EXTERNAL_PAPER_CAMPAIGN_OBSERVATION_V1,
            campaign_id,
            evidence.evidence_id,
            evidence.observation_type,
            known,
            unknown_broker_activity,
        )
        return cls(
            deterministic_id("external_paper_campaign_observation", payload),
            campaign_id,
            evidence.evidence_id,
            evidence.observation_type,
            known,
            unknown_broker_activity,
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ExternalPaperInternalExecutionBinding:
    """Immutable bridge to the existing pending Securities PAPER execution."""

    binding_id: str
    authorization_id: str
    instruction_id: str
    campaign_id: str
    paper_trial_authorization_id: str
    portfolio_instruction_id: str
    securities_paper_run_id: str
    internal_order_id: str
    risk_reservation_id: str
    execution_policy_id: str
    execution_policy_hash: str
    instrument_id: str
    symbol: str
    side: str
    intended_quantity: Decimal
    intended_notional: Decimal
    intended_session: str
    expected_execution_hash: str | None
    idempotency_key: str
    state: ExternalPaperInternalExecutionBindingState
    canonical_hash: str

    @classmethod
    def pending(
        cls,
        *,
        authorization: ExternalPaperAuthorization,
        instruction: ExternalPaperInstruction,
        securities_paper_run_id: str,
        internal_order_id: str,
        risk_reservation_id: str,
        execution_policy_id: str,
        execution_policy_hash: str,
        instrument_id: str,
        intended_session: str,
        idempotency_key: str,
        expected_execution_hash: str | None = None,
    ) -> ExternalPaperInternalExecutionBinding:
        if (
            instruction.authorization_id != authorization.authorization_id
            or instruction.campaign_id != authorization.campaign_id
            or not all(
                (
                    securities_paper_run_id,
                    internal_order_id,
                    risk_reservation_id,
                    execution_policy_id,
                    execution_policy_hash,
                    instrument_id,
                    intended_session,
                    idempotency_key,
                )
            )
        ):
            raise ValueError("external PAPER internal execution binding is invalid")
        payload = (
            APEX_EXTERNAL_PAPER_INTERNAL_EXECUTION_BINDING_V1,
            authorization.authorization_id,
            instruction.instruction_id,
            authorization.campaign_id,
            authorization.paper_trial_authorization_id,
            authorization.portfolio_instruction_id,
            securities_paper_run_id,
            internal_order_id,
            risk_reservation_id,
            execution_policy_id,
            execution_policy_hash,
            instrument_id,
            instruction.symbol,
            instruction.side,
            instruction.quantity,
            instruction.maximum_notional,
            intended_session,
            expected_execution_hash,
            idempotency_key,
            ALPACA_PAPER_BROKER_V1,
        )
        return cls(
            deterministic_id("external_paper_internal_execution_binding", payload),
            authorization.authorization_id,
            instruction.instruction_id,
            authorization.campaign_id,
            authorization.paper_trial_authorization_id,
            authorization.portfolio_instruction_id,
            securities_paper_run_id,
            internal_order_id,
            risk_reservation_id,
            execution_policy_id,
            execution_policy_hash,
            instrument_id,
            instruction.symbol,
            instruction.side,
            instruction.quantity,
            instruction.maximum_notional,
            intended_session,
            expected_execution_hash,
            idempotency_key,
            ExternalPaperInternalExecutionBindingState.ORDER_PENDING_EXTERNAL_FILL,
            content_digest(payload),
        )

    def transition(
        self, state: ExternalPaperInternalExecutionBindingState
    ) -> ExternalPaperInternalExecutionBinding:
        allowed = {
            ExternalPaperInternalExecutionBindingState.ORDER_PENDING_EXTERNAL_FILL: {
                ExternalPaperInternalExecutionBindingState.PARTIALLY_APPLIED,
                ExternalPaperInternalExecutionBindingState.FULLY_APPLIED,
                ExternalPaperInternalExecutionBindingState.CANCELED,
                ExternalPaperInternalExecutionBindingState.REJECTED,
                ExternalPaperInternalExecutionBindingState.BLOCKED_AMBIGUOUS,
                ExternalPaperInternalExecutionBindingState.BLOCKED_RECONCILIATION,
            },
            ExternalPaperInternalExecutionBindingState.PARTIALLY_APPLIED: {
                ExternalPaperInternalExecutionBindingState.FULLY_APPLIED,
                ExternalPaperInternalExecutionBindingState.CANCELED,
                ExternalPaperInternalExecutionBindingState.BLOCKED_RECONCILIATION,
            },
        }
        if state not in allowed.get(self.state, set()):
            raise ValueError("external PAPER internal execution binding transition is invalid")
        return type(self)(
            self.binding_id,
            self.authorization_id,
            self.instruction_id,
            self.campaign_id,
            self.paper_trial_authorization_id,
            self.portfolio_instruction_id,
            self.securities_paper_run_id,
            self.internal_order_id,
            self.risk_reservation_id,
            self.execution_policy_id,
            self.execution_policy_hash,
            self.instrument_id,
            self.symbol,
            self.side,
            self.intended_quantity,
            self.intended_notional,
            self.intended_session,
            self.expected_execution_hash,
            self.idempotency_key,
            state,
            self.canonical_hash,
        )


@dataclass(frozen=True, slots=True)
class ValidatedExternalPaperFill:
    """Immutable broker-fill validation.  It is evidence, not accounting by itself."""

    validated_fill_id: str
    instruction_id: str
    authorization_id: str
    submission_attempt_id: str
    broker_order_id: str
    evidence_id: str
    evidence_hash: str
    symbol: str
    side: str
    cumulative_quantity: Decimal
    newly_observed_quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    occurred_at: datetime
    state: ValidatedExternalPaperFillState
    reason_code: str | None
    canonical_hash: str

    @classmethod
    def observed(
        cls,
        *,
        instruction: ExternalPaperInstruction,
        authorization: ExternalPaperAuthorization,
        submission_attempt: ExternalPaperSubmissionAttempt,
        evidence: ExternalPaperBrokerEvidence,
        prior_cumulative_quantity: str = "0",
    ) -> ValidatedExternalPaperFill:
        payload = evidence.sanitized_payload
        broker_order_id = payload.get("id")
        quantity_text = payload.get("filled_qty")
        price_text = payload.get("filled_avg_price")
        if not broker_order_id or quantity_text is None or price_text is None:
            raise ValueError("external PAPER fill evidence is incomplete")
        cumulative = parse_decimal(quantity_text)
        prior = parse_decimal(prior_cumulative_quantity)
        price = parse_decimal(price_text, allow_zero=False)
        fee = parse_decimal(payload.get("fee", "0") or "0")
        if cumulative <= prior or cumulative > instruction.quantity:
            raise ValueError("external PAPER cumulative fill quantity is invalid")
        if (
            payload.get("symbol") != instruction.symbol
            or payload.get("side") != instruction.side.lower()
        ):
            raise ValueError("external PAPER fill identity does not match instruction")
        if submission_attempt.state is ExternalPaperSubmissionState.AMBIGUOUS:
            state = ValidatedExternalPaperFillState.BLOCKED_AMBIGUOUS
            reason = "SUBMISSION_AMBIGUOUS"
        else:
            state = ValidatedExternalPaperFillState.OBSERVED
            reason = None
        occurred = evidence.broker_reported_at or evidence.observed_at
        source = (
            APEX_VALIDATED_EXTERNAL_PAPER_FILL_V1,
            instruction.instruction_id,
            authorization.authorization_id,
            submission_attempt.attempt_id,
            broker_order_id,
            evidence.evidence_id,
            evidence.canonical_hash,
            instruction.symbol,
            instruction.side,
            cumulative,
            cumulative - prior,
            price,
            fee,
            occurred,
        )
        return cls(
            deterministic_id("validated_external_paper_fill", source),
            instruction.instruction_id,
            authorization.authorization_id,
            submission_attempt.attempt_id,
            broker_order_id,
            evidence.evidence_id,
            evidence.canonical_hash,
            instruction.symbol,
            instruction.side,
            cumulative,
            cumulative - prior,
            price,
            fee,
            occurred,
            state,
            reason,
            content_digest(source),
        )

    def validate(self) -> ValidatedExternalPaperFill:
        if self.state is not ValidatedExternalPaperFillState.OBSERVED:
            return self
        return type(self)(
            self.validated_fill_id,
            self.instruction_id,
            self.authorization_id,
            self.submission_attempt_id,
            self.broker_order_id,
            self.evidence_id,
            self.evidence_hash,
            self.symbol,
            self.side,
            self.cumulative_quantity,
            self.newly_observed_quantity,
            self.fill_price,
            self.fee,
            self.occurred_at,
            ValidatedExternalPaperFillState.VALIDATED,
            None,
            self.canonical_hash,
        )

    def applied(self) -> ValidatedExternalPaperFill:
        if self.state is not ValidatedExternalPaperFillState.VALIDATED:
            raise ValueError("only validated external PAPER fills may be applied")
        return type(self)(
            self.validated_fill_id,
            self.instruction_id,
            self.authorization_id,
            self.submission_attempt_id,
            self.broker_order_id,
            self.evidence_id,
            self.evidence_hash,
            self.symbol,
            self.side,
            self.cumulative_quantity,
            self.newly_observed_quantity,
            self.fill_price,
            self.fee,
            self.occurred_at,
            ValidatedExternalPaperFillState.APPLIED,
            None,
            self.canonical_hash,
        )


def reconcile_external_paper(
    *, instruction: ExternalPaperInstruction, broker_order: dict[str, str] | None
) -> ExternalPaperReconciliation:
    if broker_order is not None and broker_order.get("id") != instruction.broker_order_id:
        classification, field, apex, broker, action = (
            ExternalPaperReconciliationClassification.MATERIAL_DISCREPANCY,
            "broker_order_id",
            instruction.broker_order_id,
            broker_order.get("id"),
            "RETRIEVE_EXACT_BROKER_ORDER_AND_REVIEW",
        )
    elif instruction.state is ExternalPaperInstructionState.RECONCILIATION_REQUIRED:
        classification, field, apex, broker, action = (
            ExternalPaperReconciliationClassification.SUBMISSION_AMBIGUOUS,
            "submission",
            instruction.state.value,
            None,
            "RETRIEVE_BY_CLIENT_ORDER_ID_AND_REVIEW",
        )
    elif broker_order is None:
        classification, field, apex, broker, action = (
            ExternalPaperReconciliationClassification.BROKER_STATE_INCOMPLETE,
            "broker_order",
            instruction.state.value,
            None,
            "SYNC_BROKER_ORDER",
        )
    elif (
        broker_order.get("symbol") != instruction.symbol
        or broker_order.get("side") != instruction.side.lower()
    ):
        classification, field, apex, broker, action = (
            ExternalPaperReconciliationClassification.MATERIAL_DISCREPANCY,
            "symbol_or_side",
            f"{instruction.symbol}:{instruction.side.lower()}",
            f"{broker_order.get('symbol')}:{broker_order.get('side')}",
            "KILL_EXTERNAL_PAPER_AND_REVIEW",
        )
    elif parse_decimal(broker_order.get("qty", str(instruction.quantity))) != instruction.quantity:
        classification, field, apex, broker, action = (
            ExternalPaperReconciliationClassification.MATERIAL_DISCREPANCY,
            "submitted_quantity",
            str(instruction.quantity),
            broker_order.get("qty"),
            "KILL_EXTERNAL_PAPER_AND_REVIEW",
        )
    else:
        classification, field, apex, broker, action = (
            ExternalPaperReconciliationClassification.RECONCILED,
            None,
            None,
            None,
            "NONE",
        )
    payload = (
        APEX_EXTERNAL_PAPER_RECONCILIATION_V1,
        instruction.instruction_id,
        classification,
        field,
        apex,
        broker,
        action,
    )
    return ExternalPaperReconciliation(
        deterministic_id("external_paper_reconciliation", payload),
        instruction.instruction_id,
        classification,
        field,
        apex,
        broker,
        action,
        content_digest(payload),
    )
